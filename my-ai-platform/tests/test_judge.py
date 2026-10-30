"""명세서 시연 시나리오(T01~T04)와 V04·V06 기대 판정을 가짜 관측 데이터로 확인한다."""

import pytest
from pydantic import ValidationError

from runner.judge import JUDGE_VERSION, MISSING, diff_observations, evaluate_check, judge_validation
from runner.models import Check, Event, Observation
from runner.models import TestCase as Case

ORDER = ["payment.checked", "inventory.decremented", "shipping.requested"]


def case(case_id: str, stock_after: int, shipping: int, order: list[str] | None = None) -> Case:
    expect = [
        {"op": "eq", "target": "response.status", "value": 200},
        {"op": "eq", "target": "state.stock.SKU-1", "value": stock_after},
        {"op": "event_count", "target": "shipping.requested", "value": shipping},
    ]
    if order:
        expect.append({"op": "event_order", "value": order})
    return Case.model_validate({"case_id": case_id, "input": {}, "expect": expect})


# 업무 규칙: 결제 완료 + 재고 충분 → 차감 후 배송 1회, 결제 실패·재고 부족 → 아무것도 하지 않음
CASES = [
    case("T01", stock_after=8, shipping=1, order=ORDER),
    case("T02", stock_after=0, shipping=1, order=ORDER),
    case("T03", stock_after=10, shipping=0),
    case("T04", stock_after=1, shipping=0),
]


def obs(env_id: str, case_id: str, stock: int, steps: list[str], status: int = 200, body=None) -> Observation:
    return Observation(
        env_id=env_id,
        case_id=case_id,
        delivered=True,
        response_status=status,
        response_body=body if body is not None else {"ok": True},
        events=[Event(step_key=s, sequence=i + 1) for i, s in enumerate(steps)],
        state={"stock": {"SKU-1": stock}},
    )


def healthy(env_id: str) -> dict[str, Observation]:
    return {
        "T01": obs(env_id, "T01", 8, ORDER),
        "T02": obs(env_id, "T02", 0, ORDER),
        "T03": obs(env_id, "T03", 10, ["payment.checked"]),
        "T04": obs(env_id, "T04", 1, ["payment.checked"]),
    }


def broken_shipping(env_id: str) -> dict[str, Observation]:
    """재고 차감 이후 배송 요청 연결이 끊긴 결함 후보."""
    o = healthy(env_id)
    o["T01"] = obs(env_id, "T01", 8, ORDER[:2])
    o["T02"] = obs(env_id, "T02", 0, ORDER[:2])
    return o


def verdicts(result) -> dict[str, str]:
    return {c.case_id: c.verdict for c in result.cases}


def test_control_group_passes():  # V03
    r = judge_validation(CASES, healthy("base"), healthy("cand"))
    assert r.overall == "pass"
    assert set(verdicts(r).values()) == {"pass"}
    assert all(c.differences == [] for c in r.cases)
    assert r.judge_version == JUDGE_VERSION


def test_missing_shipping_is_regression_even_with_200():  # V04
    r = judge_validation(CASES, healthy("base"), broken_shipping("cand"))
    assert verdicts(r) == {
        "T01": "regression_suspected",
        "T02": "regression_suspected",
        "T03": "pass",
        "T04": "pass",
    }
    assert r.overall == "regression_suspected"
    assert r.counts == {"pass": 2, "regression_suspected": 2, "baseline_needs_check": 0, "unverified": 0}

    t01 = r.cases[0]
    failed = [c for c in t01.cand.checks if not c.passed]
    assert [(c.check.op, c.actual) for c in failed] == [("event_count", 0), ("event_order", ORDER[:2])]
    # 응답은 양쪽 모두 정상 → 차이는 배송 이벤트에만 있다
    assert {d.path for d in t01.differences} == {"events.count.shipping.requested", "events.order"}
    count_diff = next(d for d in t01.differences if d.path == "events.count.shipping.requested")
    assert (count_diff.base, count_diff.cand) == (1, 0)


def test_baseline_violation_is_not_called_regression():  # V06
    base = broken_shipping("base")
    r = judge_validation(CASES, base, broken_shipping("cand"))
    assert verdicts(r)["T01"] == "baseline_needs_check"
    assert r.overall == "baseline_needs_check"


def test_undelivered_or_missing_observation_is_unverified():
    cand = healthy("cand")
    cand["T01"] = Observation(env_id="cand", case_id="T01", delivered=False, error="timeout: ReadTimeout")
    del cand["T02"]
    r = judge_validation(CASES, healthy("base"), cand)
    v = verdicts(r)
    assert v["T01"] == v["T02"] == "unverified"
    assert r.cases[0].cand.reason == "timeout: ReadTimeout"
    assert r.overall == "incomplete"  # 미검증은 전체 통과로 집계하지 않는다


def test_regression_outranks_unverified_in_overall():
    cand = broken_shipping("cand")
    cand["T03"] = Observation(env_id="cand", case_id="T03", delivered=False)
    r = judge_validation(CASES, healthy("base"), cand)
    assert r.overall == "regression_suspected"
    assert r.counts["unverified"] == 1


def test_case_without_checks_is_not_passed():
    empty = Case(case_id="T09", input={})
    r = judge_validation([empty], {"T09": obs("base", "T09", 1, [])}, {"T09": obs("cand", "T09", 1, [])})
    assert verdicts(r) == {"T09": "unverified"}
    assert r.overall == "incomplete"


def test_no_cases_is_incomplete():
    assert judge_validation([], {}, {}).overall == "incomplete"


def test_eq_missing_path_and_strict_bool():
    o = obs("base", "T01", 8, [], body={"accepted": True, "items": [{"qty": 2}]})
    missing = evaluate_check(Check(op="eq", target="state.stock.SKU-9", value=0), o)
    assert not missing.passed and missing.actual == MISSING
    assert not evaluate_check(Check(op="eq", target="response.body.accepted", value=1), o).passed
    assert evaluate_check(Check(op="eq", target="response.body.accepted", value=True), o).passed
    assert evaluate_check(Check(op="eq", target="response.body.items.0.qty", value=2), o).passed


def test_gte():
    o = obs("base", "T01", 8, [], body={"note": "x"})
    assert evaluate_check(Check(op="gte", target="state.stock.SKU-1", value=8), o).passed
    assert not evaluate_check(Check(op="gte", target="state.stock.SKU-1", value=9), o).passed
    r = evaluate_check(Check(op="gte", target="response.body.note", value=1), o)
    assert not r.passed and r.reason == "actual value is not a number"


def test_event_order_detects_duplicates_and_uses_sequence():
    o = obs("base", "T01", 8, [])
    o.events = [
        Event(step_key="shipping.requested", sequence=3),
        Event(step_key="payment.checked", sequence=1),
        Event(step_key="inventory.decremented", sequence=2),
    ]
    assert evaluate_check(Check(op="event_order", value=ORDER), o).passed
    o.events.append(Event(step_key="shipping.requested", sequence=4))
    assert not evaluate_check(Check(op="event_order", value=ORDER), o).passed


def test_diff_ignores_volatile_keys():
    base = obs("base", "T01", 8, ORDER, body={"shipment_id": "SHP-1", "at": "10:00", "total": 2})
    cand = obs("cand", "T01", 8, ORDER, body={"shipment_id": "SHP-2", "at": "10:01", "total": 3})
    diffs = diff_observations(base, cand, ignore_keys={"shipment_id", "at"})
    assert [(d.path, d.base, d.cand) for d in diffs] == [("response.body.total", 2, 3)]


def test_diff_marks_missing_fields():
    base = obs("base", "T01", 8, [], body={"a": 1})
    cand = obs("cand", "T01", 8, [], body={})
    assert [(d.path, d.base, d.cand) for d in diff_observations(base, cand)] == [("response.body.a", 1, MISSING)]


@pytest.mark.parametrize(
    "check",
    [
        {"op": "gte", "target": "x", "value": "1"},
        {"op": "event_count", "target": "x", "value": -1},
        {"op": "event_count", "target": "x", "value": True},
        {"op": "event_order", "value": []},
        {"op": "event_order", "value": "shipping.requested"},
        {"op": "contains", "target": "x", "value": 1},
    ],
)
def test_invalid_checks_are_rejected(check):
    with pytest.raises(ValidationError):
        Check.model_validate(check)
