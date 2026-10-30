import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from ossguard.commands import command
from ossguard.errors import GuardError
from ossguard.evidence import Redactor, bounded_text
from ossguard.schemas import EnvironmentUpdate, ExecutionProfile, HttpCase, ProjectCreate
from ossguard.updater import package_diff
from ossguard.validator import assert_case, compare_results, normalize, read_journal


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/team/app",
        "https://github.com/team/app/x",
        "https://github.com/team/..",
        "https://evil.com/team/app",
    ],
)
def test_reject_non_repository_urls(url):
    with pytest.raises(ValidationError):
        ProjectCreate(name="demo", repository_url=url)


@pytest.mark.parametrize(
    "path",
    [
        "//evil.com",
        "https://evil.com",
        "/\\evil",
        "/x\r\nHost: evil",
        "/../mock/__admin",
        "/%2e%2e/mock/__admin",
    ],
)
def test_reject_external_test_path(path):
    with pytest.raises(ValidationError):
        HttpCase(id="x", path=path)


def test_environment_requires_secret_references():
    with pytest.raises(ValidationError):
        ExecutionProfile(env_schema={"API_KEY": {"secret": False}})
    with pytest.raises(ValidationError):
        EnvironmentUpdate(secret_refs={"API_KEY": "projects/p/secrets/s/versions/latest"})


def test_shipping_omission_with_http_200_is_failure():
    case = HttpCase(
        id="x",
        path="/orders",
        assertions=[{"path": "stock", "value": 8}],
        external=[{"path": "/shipping", "count": 1}],
    )
    checks = assert_case(case, 200, {"stock": 8}, [])
    assert checks[0]["passed"] is True
    assert checks[-1]["actual"] == 0
    assert checks[-1]["passed"] is False
    assert compare_results([{"passed": True}], [{"passed": False}]) == "REGRESSION_SUSPECTED"
    assert compare_results([{"passed": False}], []) == "BASELINE_INVALID"
    assert compare_results([{"passed": True}], []) == "NOT_EVALUATED"


def test_mock_unmatched_404_does_not_count_as_success():
    case = HttpCase(id="x", path="/orders", external=[{"path": "/shipping", "count": 1}])
    checks = assert_case(
        case,
        200,
        {},
        [
            {
                "request": {"method": "POST", "url": "/shipping"},
                "wasMatched": False,
                "response_status": 404,
            }
        ],
    )
    assert not all(c["passed"] for c in checks)


@pytest.mark.parametrize("environment", ["baseline", "candidate"])
def test_request_timeout_is_not_a_business_regression(environment):
    baseline = [{"passed": True}]
    candidate = [{"passed": True}]
    (baseline if environment == "baseline" else candidate)[0] = {
        "passed": False,
        "request_error": "ReadTimeout",
    }
    assert compare_results(baseline, candidate) == "NOT_EVALUATED"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"requests": [], "requestJournalDisabled": True}),
    ],
)
async def test_observer_failure_is_not_zero_requests(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(GuardError, match="mock journal"):
            await read_journal(client, "http://mock", 10000)


def test_masking_bounding_and_ignored_fields():
    redactor = Redactor(["private-value"])
    value = redactor.clean({"Authorization": "Bearer hi", "nested": {"log": "private-value"}})
    assert "private-value" not in json.dumps(value)
    assert value["Authorization"] == "[REDACTED]"
    tail = bounded_text("a" * 1000, 30, redactor)
    assert tail["truncated"] and tail["retained_bytes"] == 30
    assert normalize({"time": 123, "stock": 8}, ["time"]) == {"stock": 8}
    assert package_diff({"a": "1"}, {"a": "2", "b": "1"}) == [
        {"name": "a", "before": "1", "after": "2"},
        {"name": "b", "before": None, "after": "1"},
    ]


async def test_subprocess_timeout_terminates_child():
    import sys

    with pytest.raises(GuardError) as exc:
        await command([sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.1)
    assert exc.value.code == "STAGE_TIMEOUT"
    await asyncio.sleep(0)
