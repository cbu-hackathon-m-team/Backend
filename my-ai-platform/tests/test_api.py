"""FastAPI 검증 API → runner 판정 엔진 연결을 시연 시나리오로 확인한다.

실행 어댑터는 app.service.ContractExecutionAdapter(모의 실행)를 그대로 쓴다.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.runner_bridge import overall_verdict
from app.schemas import CommonExecutionResult, VerdictType

client = TestClient(main.app)


def setup_suite(confirm: bool = True, cases=None, **project) -> tuple[str, str]:
    p = client.post("/api/projects", json=project).json()
    client.put(f"/api/projects/{p['id']}/rules", json={})
    s = client.post(f"/api/projects/{p['id']}/test-suites/generate").json()
    client.put(f"/api/test-suites/{s['id']}", json={"cases": cases or s["cases"], "confirmed": confirm})
    return p["id"], s["id"]


def validate(project_id: str, suite_id: str):
    return client.post("/api/validations", json={"project_id": project_id, "test_suite_id": suite_id})


def run(**project) -> dict:
    r = validate(*setup_suite(**project))
    assert r.status_code == 202, r.text
    return client.get(f"/api/validations/{r.json()['validation_id']}").json()


def verdicts(v: dict) -> dict[str, str]:
    return {c["case_id"]: c["verdict"] for c in v["case_results"]}


@pytest.fixture(autouse=True)
def release_lock():
    main.ACTIVE_VALIDATION_LOCK["is_running"] = False
    yield
    main.ACTIVE_VALIDATION_LOCK["is_running"] = False


def test_missing_shipping_candidate_is_regression():  # V04
    v = run(candidate_artifact_id="flow_defect_missing_shipping")
    assert verdicts(v) == {
        "T01": "REGRESSION_SUSPECTED",
        "T02": "REGRESSION_SUSPECTED",
        "T03": "PASS",
        "T04": "PASS",
    }
    assert v["overall_verdict"] == "REGRESSION_SUSPECTED"
    assert v["comparator_version"] == "runner-judge-1"

    t01 = v["case_results"][0]
    assert t01["violated_rules"] == ["[event_count 위반] shipping_request: 기대=1, 실제=0"]
    assert "events.count.shipping_request: 기준=1 / 후보=0" in t01["diffs"]
    assert t01["judgement"]["base"]["status"] == "pass"
    assert t01["judgement"]["cand"]["status"] == "fail"


def test_control_group_and_fixed_flow_pass():  # V03, V05
    for artifact in ("flow_normal_v1", "flow_fixed_v2"):
        v = run(candidate_artifact_id=artifact)
        assert set(verdicts(v).values()) == {"PASS"}, artifact
        assert v["overall_verdict"] == "PASS"
        assert all(c["diffs"] == [] for c in v["case_results"])


def test_broken_baseline_needs_check():  # V06
    v = run(base_artifact_id="flow_broken_base", candidate_artifact_id="flow_normal_v1")
    assert set(verdicts(v).values()) == {"BASE_CHECK_REQUIRED"}
    assert v["case_results"][0]["violated_rules"] == [
        "[eq 위반] response.body.remaining_stock: 기대=8, 실제=-999"
    ]


def test_execution_error_is_not_judged(monkeypatch):
    original = main.adapter.execute_and_collect

    def failing(run_id, env_id, artifact_id, case):
        res = original(run_id, env_id, artifact_id, case)
        if env_id == "candidate" and case.case_id == "T01":
            return CommonExecutionResult(**{**res.model_dump(), "execution_error": "timeout: ReadTimeout"})
        return res

    monkeypatch.setattr(main.adapter, "execute_and_collect", failing)
    v = run(candidate_artifact_id="flow_normal_v1")
    assert verdicts(v)["T01"] == "EXECUTION_ERROR"
    assert v["case_results"][0]["violated_rules"] == ["[candidate] timeout: ReadTimeout"]
    assert v["overall_verdict"] == "EXECUTION_ERROR"


def test_regression_outranks_execution_error_in_overall(monkeypatch):
    original = main.adapter.execute_and_collect

    def failing(run_id, env_id, artifact_id, case):
        res = original(run_id, env_id, artifact_id, case)
        if env_id == "candidate" and case.case_id == "T03":
            return CommonExecutionResult(**{**res.model_dump(), "execution_error": "connection refused"})
        return res

    monkeypatch.setattr(main.adapter, "execute_and_collect", failing)
    v = run(candidate_artifact_id="flow_defect_missing_shipping")
    assert verdicts(v) == {
        "T01": "REGRESSION_SUSPECTED",
        "T02": "REGRESSION_SUSPECTED",
        "T03": "EXECUTION_ERROR",
        "T04": "PASS",
    }
    assert v["overall_verdict"] == "REGRESSION_SUSPECTED"


@pytest.mark.parametrize(
    "case_verdicts, expected",
    [
        (["PASS", "PASS"], "PASS"),
        (["PASS", "UNVERIFIED"], "UNVERIFIED"),
        (["UNVERIFIED", "EXECUTION_ERROR"], "EXECUTION_ERROR"),
        (["EXECUTION_ERROR", "BASE_CHECK_REQUIRED"], "BASE_CHECK_REQUIRED"),
        (["BASE_CHECK_REQUIRED", "REGRESSION_SUSPECTED", "EXECUTION_ERROR"], "REGRESSION_SUSPECTED"),
        ([], "UNVERIFIED"),
    ],
)
def test_overall_verdict_priority(case_verdicts, expected):
    assert overall_verdict([VerdictType(v) for v in case_verdicts]) == VerdictType(expected)


def test_unconfirmed_suite_is_blocked():  # V01
    r = validate(*setup_suite(confirm=False))
    assert r.status_code == 400


def test_invalid_expectation_is_blocked_before_running():
    bad = [{
        "case_id": "T01",
        "description": "잘못된 기대값",
        "inputs": {"payment_status": "COMPLETED", "stock": 10, "order_qty": 2},
        "expectations": [{"operator": "event_count", "target_field_or_step": "shipping_request", "expected_value": "한 번"}],
    }]
    r = validate(*setup_suite(cases=bad))
    assert r.status_code == 400
    assert "기대 조건 형식" in r.json()["detail"]
    assert main.ACTIVE_VALIDATION_LOCK["is_running"] is False


def test_duplicate_start_is_rejected():  # V07
    ids = setup_suite()
    main.ACTIVE_VALIDATION_LOCK["is_running"] = True
    assert validate(*ids).status_code == 409
