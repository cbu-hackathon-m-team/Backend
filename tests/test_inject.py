"""모조 환경 없이 httpx.MockTransport로 주입 동작을 확인한다."""

import asyncio

import httpx
import pytest

from runner.inject import (
    HEADER_CASE_ID,
    HEADER_ENV_ID,
    HEADER_RUN_ID,
    EmptySuiteError,
    UnconfirmedSuiteError,
    build_mock_inputs,
    inject_suite,
)
from runner.models import EnvTarget
from runner.models import TestSuite as Suite

SUITE = {
    "id": "suite-1",
    "project_id": "proj-1",
    "version": 2,
    "confirmed": True,
    "cases": [
        {
            "case_id": "T01",
            "input": {"order_id": "ORD-T01", "sku": "SKU-1", "qty": 2},
            "initial_state": {"stock": {"SKU-1": 10}, "payments": {"ORD-T01": "paid"}},
            "expect": [{"op": "event_count", "target": "shipping.requested", "value": 1}],
        },
        {
            "case_id": "T03",
            "input": {"order_id": "ORD-T03", "sku": "SKU-1", "qty": 2},
            "initial_state": {"stock": {"SKU-1": 10}, "payments": {"ORD-T03": "failed"}},
        },
    ],
}

BASE = EnvTarget(env_id="base", input_url="http://base/in", reset_url="http://base/reset")
CAND = EnvTarget(env_id="cand", input_url="http://cand/in", reset_url="http://cand/reset")


def run(targets, mocks, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await inject_suite(targets, mocks, client)

    return asyncio.run(go())


def test_unconfirmed_suite_is_blocked():
    suite = Suite.model_validate({**SUITE, "confirmed": False})
    with pytest.raises(UnconfirmedSuiteError):
        build_mock_inputs(suite, run_id="run-1")


def test_empty_suite_is_blocked():
    suite = Suite.model_validate({**SUITE, "cases": []})
    with pytest.raises(EmptySuiteError):
        build_mock_inputs(suite, run_id="run-1")


def test_mock_inputs_are_detached_from_suite():
    suite = Suite.model_validate(SUITE)
    mocks = build_mock_inputs(suite, run_id="run-1")
    suite.cases[0].input["qty"] = 99
    suite.cases[0].initial_state["stock"]["SKU-1"] = 0
    assert mocks[0].input["qty"] == 2
    assert mocks[0].initial_state["stock"]["SKU-1"] == 10
    assert (mocks[0].suite_id, mocks[0].suite_version) == ("suite-1", 2)


def test_same_input_goes_to_both_envs_in_order_with_seed_first():
    calls = []

    def handler(req: httpx.Request):
        calls.append((str(req.url), req.headers[HEADER_RUN_ID], req.headers[HEADER_ENV_ID], req.headers[HEADER_CASE_ID]))
        if req.url.path == "/in":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200)

    mocks = build_mock_inputs(Suite.model_validate(SUITE), run_id="run-1")
    results = run([BASE, CAND], mocks, handler)

    assert [c[0] for c in calls] == [
        "http://base/reset", "http://base/in", "http://cand/reset", "http://cand/in",
        "http://base/reset", "http://base/in", "http://cand/reset", "http://cand/in",
    ]
    assert calls[0][1:] == ("run-1", "base", "T01")
    assert calls[3][1:] == ("run-1", "cand", "T01")
    assert [(r.env_id, r.case_id) for r in results] == [("base", "T01"), ("cand", "T01"), ("base", "T03"), ("cand", "T03")]
    assert all(r.delivered and r.response_status == 200 and r.response_body == {"ok": True} for r in results)


def test_error_status_is_recorded_not_treated_as_delivery_failure():
    def handler(req):
        return httpx.Response(500, text="boom") if req.url.path == "/in" else httpx.Response(200)

    mocks = build_mock_inputs(Suite.model_validate(SUITE), run_id="run-1")
    r = run([BASE], mocks[:1], handler)[0]
    assert r.delivered and r.response_status == 500 and r.response_body == "boom"


def test_seed_failure_skips_input():
    sent = []

    def handler(req):
        sent.append(req.url.path)
        return httpx.Response(503) if req.url.path == "/reset" else httpx.Response(200)

    mocks = build_mock_inputs(Suite.model_validate(SUITE), run_id="run-1")
    r = run([BASE], mocks[:1], handler)[0]
    assert not r.delivered and r.failed_stage == "seed"
    assert sent == ["/reset"]


def test_missing_reset_url_blocks_case_with_initial_state():
    target = EnvTarget(env_id="base", input_url="http://base/in")
    mocks = build_mock_inputs(Suite.model_validate(SUITE), run_id="run-1")
    r = run([target], mocks[:1], lambda req: httpx.Response(200))[0]
    assert not r.delivered and r.failed_stage == "seed"


def test_timeout_is_recorded_as_input_failure():
    def handler(req):
        if req.url.path == "/in":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200)

    mocks = build_mock_inputs(Suite.model_validate(SUITE), run_id="run-1")
    r = run([BASE], mocks[:1], handler)[0]
    assert not r.delivered and r.failed_stage == "input" and r.error.startswith("timeout")
