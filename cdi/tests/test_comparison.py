import pytest
from pydantic import ValidationError

from ossguard.comparison import compare, evaluate
from ossguard.contracts import Case, Event, Evidence, Expectation


def result(**kwargs):
    return Evidence(run_id="test", env_id="baseline", case_id="T", status="COMPLETE", **kwargs)


def test_generic_fields_and_types():
    case = Case(id="T", input={}, expectations=[Expectation(op="gte", path="score", expected=2)])
    assert evaluate(case, result(response={"score": 3}))["status"] == "PASS"
    assert evaluate(case, result(response={"score": "3"}))["status"] == "VIOLATION"
    assert evaluate(case, result(response={"other": 3}))["checks"][0]["missing"]


def test_order_uses_observed_sequence():
    case = Case(id="T", input={}, expectations=[Expectation(op="event_order", expected=["alpha", "beta"])])
    events = [
        Event(step_key="beta", event_type="done", sequence=1),
        Event(step_key="alpha", event_type="done", sequence=2),
    ]
    assert evaluate(case, result(events=events))["status"] == "VIOLATION"


def test_difference_is_separate_from_rule_violation():
    case = Case(id="T", input={}, expectations=[Expectation(op="gte", path="score", expected=2)])
    before, after = result(response={"score": 3}), result(response={"score": 4})
    comparison = compare(case, before, after, [])
    assert comparison["verdict"] == "PASS" and comparison["different"]


def test_ignored_fields_do_not_mutate_original_evidence():
    case = Case(id="T", input={}, expectations=[Expectation(op="eq", path="score", expected=2)])
    before = result(response={"score": 2, "timestamp": "first"})
    after = result(response={"score": 2, "timestamp": "second"})
    assert not compare(case, before, after, ["timestamp"])["different"]
    assert before.response["timestamp"] == "first"


@pytest.mark.parametrize(
    "rule",
    [
        {"op": "shell", "expected": "bad"},
        {"op": "eq", "expected": 1},
        {"op": "event_count", "expected": -1},
        {"op": "gte", "path": "x", "expected": True},
        {"op": "event_order", "expected": "alpha"},
    ],
)
def test_reject_invalid_comparison_language(rule):
    with pytest.raises(ValidationError):
        Expectation.model_validate(rule)
