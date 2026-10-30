import copy
import json
from urllib.parse import urlsplit

import httpx

from ossguard.errors import GuardError
from ossguard.schemas import HttpCase


def json_path(value, path: str):
    if not path:
        return value
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        else:
            value = value[part]
    return value


def normalize(value, ignored: list[str]):
    result = copy.deepcopy(value)
    for path in ignored:
        parts = path.split(".")
        try:
            parent = json_path(result, ".".join(parts[:-1]))
            if isinstance(parent, dict):
                parent.pop(parts[-1], None)
            elif isinstance(parent, list) and parts[-1].isdigit():
                parent[int(parts[-1])] = "[IGNORED]"
        except (KeyError, IndexError, TypeError):
            pass
    return result


def matches(event: dict, expected):
    request = event["request"]
    if request["method"] != expected.method or urlsplit(request["url"]).path != expected.path:
        return False
    if expected.body_contains:
        try:
            body = json.loads(request.get("body", "{}"))
            return isinstance(body, dict) and all(
                body.get(k) == v for k, v in expected.body_contains.items()
            )
        except (TypeError, ValueError):
            return False
    return True


def assert_case(case: HttpCase, status: int, body, events: list[dict]):
    checks = [
        {
            "rule": "http_status",
            "expected": case.expected_status,
            "actual": status,
            "passed": case.expected_status == status,
        }
    ]
    for assertion in case.assertions:
        actual = None
        passed = False
        try:
            actual = json_path(body, assertion.path)
            if assertion.op == "eq":
                passed = type(actual) is type(assertion.value) and actual == assertion.value
            elif isinstance(actual, (int, float)) and not isinstance(actual, bool):
                passed = actual >= assertion.value
        except (TypeError, IndexError, KeyError):
            pass
        checks.append(
            {
                "rule": f"json.{assertion.path}.{assertion.op}",
                "expected": assertion.value,
                "actual": actual,
                "passed": passed,
            }
        )
    for expected in case.external:
        selected = [event for event in events if matches(event, expected)]
        valid = all(
            event.get("wasMatched") is True and event["response_status"] == expected.response_status
            for event in selected
        )
        checks.append(
            {
                "rule": f"external.{expected.method}.{expected.path}",
                "expected": expected.count,
                "actual": len(selected),
                "passed": len(selected) == expected.count and valid,
                "all_matched": valid,
            }
        )
    if any(not event.get("wasMatched") for event in events):
        checks.append({"rule": "no_unmatched_mock_requests", "passed": False})
    return checks


async def read_journal(client: httpx.AsyncClient, mock_url: str, limit: int):
    try:
        async with client.stream("GET", mock_url + "/__admin/requests") as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise GuardError("OBSERVATION_ERROR", "Mock journal exceeds observation limit")
        data = json.loads(content)
        if data.get("requestJournalDisabled") is True or not isinstance(data.get("requests"), list):
            raise ValueError("journal not available")
        events = data["requests"]
        if len(events) >= 1000:
            raise ValueError("journal may have dropped requests")
        for event in events:
            request = event["request"]
            if not isinstance(request["method"], str) or not isinstance(request["url"], str):
                raise ValueError("invalid request")
            if not isinstance(event.get("wasMatched"), bool):
                raise ValueError("match status unavailable")
            event["response_status"] = event.get("responseDefinition", {}).get("status")
            if event["response_status"] is None:
                event["response_status"] = event.get("response", {}).get("status")
            if not isinstance(event["response_status"], int):
                raise ValueError("mock response status unavailable")
        return events
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise GuardError("OBSERVATION_ERROR", "Cannot reliably read the mock journal") from exc


async def execute_case(
    client: httpx.AsyncClient, app_url: str, mock_url: str, case: HttpCase, limit: int
):
    try:
        reset = await client.delete(mock_url + "/__admin/requests")
        reset.raise_for_status()
    except httpx.HTTPError as exc:
        raise GuardError("OBSERVATION_ERROR", "Cannot reset the mock journal") from exc
    request_error = None
    status = 0
    body = None
    try:
        async with client.stream(case.method, app_url + case.path, json=case.body) as response:
            status = response.status_code
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise GuardError("RESPONSE_TOO_LARGE", "Application response exceeds limit")
        try:
            body = json.loads(content)
        except ValueError:
            body = {"non_json": content.decode(errors="replace")}
    except httpx.HTTPError as exc:
        request_error = type(exc).__name__
    # Even when an application times out, never substitute a failed observer with an empty list.
    events = await read_journal(client, mock_url, limit * 4)
    checks = assert_case(case, status, body, events)
    return {
        "case_id": case.id,
        "status_code": status,
        "body": body,
        "normalized_body": normalize(body, case.ignore_paths),
        "events": events,
        "checks": checks,
        "request_error": request_error,
        "passed": not request_error and all(c["passed"] for c in checks),
    }


def compare_results(baseline: list[dict], candidate: list[dict]):
    if any(result.get("request_error") for result in [*baseline, *candidate]):
        return "NOT_EVALUATED"
    if not baseline or not all(result["passed"] for result in baseline):
        return "BASELINE_INVALID"
    if len(candidate) != len(baseline):
        return "NOT_EVALUATED"
    return "PASS" if all(result["passed"] for result in candidate) else "REGRESSION_SUSPECTED"
