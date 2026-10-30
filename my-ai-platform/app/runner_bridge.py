"""API 스키마와 검증 실행기(runner) 사이의 변환.

- TestCaseItem             → runner TestCase    (사용자가 확정한 판정 기준)
- CommonExecutionResult    → runner Observation (어댑터가 수집한 관측 결과)
- runner CaseVerdict       → VerdictType + 위반 내용 + 전후 차이
"""

from typing import Any, Dict, List, Tuple

from runner.judge import judge_case
from runner.models import Check, CheckResult, Event, Observation, TestCase

from app.schemas import CommonExecutionResult, TestCaseItem, VerdictType

_VERDICTS = {
    "pass": VerdictType.PASS,
    "regression_suspected": VerdictType.REGRESSION_SUSPECTED,
    "baseline_needs_check": VerdictType.BASE_CHECK_REQUIRED,
}


# 전체 판정 우선순위 (runner.judge.judge_validation과 같은 순서)
# 회귀 의심을 가장 먼저 드러내고, 실행 오류·미검증이 하나라도 있으면 통과로 집계하지 않는다
_OVERALL_PRIORITY = [
    VerdictType.REGRESSION_SUSPECTED,
    VerdictType.BASE_CHECK_REQUIRED,
    VerdictType.EXECUTION_ERROR,
    VerdictType.UNVERIFIED,
]


def overall_verdict(verdicts: List[VerdictType]) -> VerdictType:
    for v in _OVERALL_PRIORITY:
        if v in verdicts:
            return v
    return VerdictType.PASS if verdicts else VerdictType.UNVERIFIED


def _target_path(op: str, target: str) -> str:
    # eq / gte 대상이 "remaining_stock"처럼 필드 이름만 있으면 응답 본문 필드로 본다
    if op in ("eq", "gte") and not target.startswith(("response.", "state.")):
        return f"response.body.{target}"
    return target


def to_runner_case(item: TestCaseItem) -> TestCase:
    """기대 조건 형식이 잘못되면 pydantic ValidationError를 낸다."""
    return TestCase(
        case_id=item.case_id,
        name=item.description,
        input=dict(item.inputs),
        expect=[
            Check(
                op=e.operator.value,
                target=_target_path(e.operator.value, e.target_field_or_step),
                value=e.expected_value,
            )
            for e in item.expectations
        ],
    )


def to_observation(res: CommonExecutionResult) -> Observation:
    return Observation(
        env_id=res.env_id,
        case_id=res.case_id,
        delivered=res.execution_error is None,
        error=res.execution_error,
        response_status=res.response.get("http_status"),
        response_body=res.response,
        events=[
            Event(step_key=e.step_key, event_type=e.event_type, payload=e.payload, sequence=e.sequence)
            for e in res.events
        ],
    )


def _violation(r: CheckResult) -> str:
    c = r.check
    expected = f">={c.value}" if c.op == "gte" else c.value
    msg = f"[{c.op} 위반] {c.target}: 기대={expected}, 실제={r.actual}"
    return f"{msg} ({r.reason})" if r.reason else msg


def judge_results(
    case: TestCase, base_res: CommonExecutionResult, cand_res: CommonExecutionResult
) -> Tuple[VerdictType, List[str], List[str], Dict[str, Any]]:
    """케이스 하나를 판정한다. (판정, 위반 내용, 전후 차이, 비교식별 상세 결과)를 돌려준다."""
    result = judge_case(case, to_observation(base_res), to_observation(cand_res))
    diffs = [f"{d.path}: 기준={d.base} / 후보={d.cand}" for d in result.differences]
    detail = result.model_dump(mode="json")

    if result.verdict == "unverified":
        reasons = [f"[{v.env_id}] {v.reason}" for v in (result.base, result.cand) if v.status == "not_run"]
        kind = VerdictType.EXECUTION_ERROR if base_res.execution_error or cand_res.execution_error else VerdictType.UNVERIFIED
        return kind, reasons, diffs, detail

    # 기준부터 위반이면 기준 쪽 위반을, 아니면 후보 쪽 위반을 보여준다
    failed_env = result.base if result.verdict == "baseline_needs_check" else result.cand
    violations = [_violation(r) for r in failed_env.checks if not r.passed]
    return _VERDICTS[result.verdict], violations, diffs, detail
