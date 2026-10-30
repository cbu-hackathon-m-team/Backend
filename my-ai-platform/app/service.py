from typing import Any, Dict, List, Tuple
from app.schemas import (
    AIAnalysisResult,
    AllowedOperator,
    CommonExecutionResult,
    ExpectationRule,
    NormalizedEvent,
    TestCaseItem,
    VerdictType,
)


class AIHelperService:
    """F03 AI 테스트 초안 생성 및 F11 AI 실패 원인 분석 모듈"""

    @staticmethod
    def generate_default_test_cases() -> List[TestCaseItem]:
        return [
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

    @staticmethod
    def analyze_failure(
        case_comparisons: List[Dict[str, Any]],
        base_artifact: str,
        candidate_artifact: str,
        simulate_ai_error: bool = False,
    ) -> AIAnalysisResult:
        if simulate_ai_error:
            return AIAnalysisResult(
                status="FAILED",
                error_message="AI API 응답 시간 초과 또는 형식 오류 (비교 판정 및 원본 증거는 정상 유지됨)",
            )

        failed_cases = [c for c in case_comparisons if c["verdict"] == VerdictType.REGRESSION_SUSPECTED]
        if not failed_cases:
            return AIAnalysisResult(
                status="SUCCESS",
                confirmed_facts=["모든 테스트 케이스(T01~T04)에서 기존 환경과 후보 환경 모두 확정된 업무 규칙을 충족했습니다."],
                root_cause_candidates=["규칙 위반 사항 없음"],
                verification_methods=["추가 조치 불필요"],
            )

        return AIAnalysisResult(
            status="SUCCESS",
            confirmed_facts=[
                f"케이스 {[c['case_id'] for c in failed_cases]}에서 HTTP 응답은 200 OK로 동일했으나, 모의 서비스의 'shipping_request(배송 요청)' 이벤트 기록이 기존 1건에서 후보 0건으로 누락되었습니다.",
                f"아티팩트 비교 결과, 기존({base_artifact}) 대비 후보({candidate_artifact})에서 'stock_deduct(node_stk_01)' 이후 'shipping_request(node_shp_01)'로의 연결 경로(wire)가 제거된 것이 확인되었습니다.",
            ],
            root_cause_candidates=[
                "Node-RED 런타임 버전 업그레이드 자체의 결함이 아니라, 후보 맞춤 흐름(JSON) 수정 과정에서 재고 차감 노드와 배송 요청 노드 간 연결이 누락되었을 가능성이 높습니다."
            ],
            verification_methods=[
                "후보 흐름 JSON에서 node_stk_01의 wires 배열에 node_shp_01을 다시 연결한 수정 샘플(flow_fixed_v2)로 재검증을 실행해 T01·T02의 배송 요청 이벤트가 1회 발생하는지 확인하십시오."
            ],
        )


class CommonComparatorEngine:
    """F09: 허용 연산(eq, gte, event_count, event_order) 기반 규칙 위반 및 전후 차이 판정 엔진"""

    @staticmethod
    def evaluate_expectations(
        exec_result: CommonExecutionResult, expectations: List[ExpectationRule]
    ) -> List[str]:
        violations = []
        for exp in expectations:
            if exp.operator == AllowedOperator.EQ:
                actual = exec_result.response.get(exp.target_field_or_step)
                if actual != exp.expected_value:
                    violations.append(f"[eq 위반] {exp.target_field_or_step}: 기대값={exp.expected_value}, 실제값={actual}")

            elif exp.operator == AllowedOperator.GTE:
                actual = exec_result.response.get(exp.target_field_or_step, 0)
                if actual < exp.expected_value:
                    violations.append(f"[gte 위반] {exp.target_field_or_step}: 기대값>={exp.expected_value}, 실제값={actual}")

            elif exp.operator == AllowedOperator.EVENT_COUNT:
                count = sum(1 for e in exec_result.events if e.step_key == exp.target_field_or_step)
                if count != exp.expected_value:
                    violations.append(
                        f"[event_count 위반] 단계 '{exp.target_field_or_step}' 호출 횟수: 기대={exp.expected_value}회, 실제={count}회"
                    )

            elif exp.operator == AllowedOperator.EVENT_ORDER:
                observed_steps = [e.step_key for e in sorted(exec_result.events, key=lambda x: x.sequence)]
                if observed_steps != exp.expected_value:
                    violations.append(f"[event_order 위반] 기대 순서={exp.expected_value}, 실제 순서={observed_steps}")
        return violations

    @classmethod
    def compare_case(
        cls,
        base_res: CommonExecutionResult,
        cand_res: CommonExecutionResult,
        expectations: List[ExpectationRule],
    ) -> Tuple[VerdictType, List[str], List[str]]:
        if base_res.execution_error or cand_res.execution_error:
            return VerdictType.EXECUTION_ERROR, ["컨테이너 실행 또는 요청 시간 초과 오류"], []

        base_violations = cls.evaluate_expectations(base_res, expectations)
        cand_violations = cls.evaluate_expectations(cand_res, expectations)

        diffs = []
        base_events = [e.step_key for e in base_res.events]
        cand_events = [e.step_key for e in cand_res.events]
        if base_events != cand_events:
            diffs.append(f"이벤트 흐름 차이: 기준={base_events} vs 후보={cand_events}")

        if base_violations:
            return VerdictType.BASE_CHECK_REQUIRED, base_violations, diffs
        if cand_violations:
            return VerdictType.REGRESSION_SUSPECTED, cand_violations, diffs

        return VerdictType.PASS, [], diffs


class ContractExecutionAdapter:
    """F06: 실행 어댑터 계약 (prepare / start / execute / collect / cleanup)"""

    def prepare(self, project: Dict[str, Any]) -> Dict[str, Any]:
        return {"prepared": True, "target_type": project["target_type"]}

    def start(self, env_spec: Dict[str, Any]) -> Dict[str, str]:
        return {"base_env_id": "nodered-base-container", "candidate_env_id": "nodered-cand-container"}

    def execute_and_collect(
        self, run_id: str, env_id: str, artifact_id: str, case: TestCaseItem
    ) -> CommonExecutionResult:
        payment = case.inputs.get("payment_status")
        stock = case.inputs.get("stock", 0)
        qty = case.inputs.get("order_qty", 0)

        events: List[NormalizedEvent] = [
            NormalizedEvent(step_key="order_received", event_type="HTTP_IN", payload={"qty": qty}, sequence=1),
            NormalizedEvent(step_key="payment_check", event_type="CHECK", payload={"status": payment}, sequence=2),
        ]

        if payment == "COMPLETED" and stock >= qty:
            remaining_stock = stock - qty
            events.append(
                NormalizedEvent(
                    step_key="stock_deduct",
                    event_type="MOCK_API_CALL",
                    payload={"deducted": qty, "remaining": remaining_stock},
                    sequence=3,
                )
            )
            if artifact_id != "flow_defect_missing_shipping":
                events.append(
                    NormalizedEvent(
                        step_key="shipping_request",
                        event_type="MOCK_API_CALL",
                        payload={"order_qty": qty, "shipped": True},
                        sequence=4,
                    )
                )
        else:
            remaining_stock = stock

        if artifact_id == "flow_broken_base":
            remaining_stock = -999

        return CommonExecutionResult(
            run_id=run_id,
            env_id=env_id,
            case_id=case.case_id,
            response={"http_status": 200, "status": "ok", "remaining_stock": remaining_stock},
            events=events,
            logs=[f"[{env_id}] Case {case.case_id} executed with artifact '{artifact_id}'"],
            step_map={
                "node_ord_01": "order_received",
                "node_pay_01": "payment_check",
                "node_stk_01": "stock_deduct",
                "node_shp_01": "shipping_request",
            },
            artifact_refs={"flow_artifact": artifact_id},
        )

    def cleanup(self, run_id: str) -> str:
        return "CLEANED_UP"