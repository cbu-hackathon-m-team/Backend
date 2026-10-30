"""Tool-independent comparison; expectations come from a confirmed snapshot."""

from copy import deepcopy
from typing import Any

from ossguard.contracts import Case, Evidence

VERSION = "1.0"
MISSING = object()


def field(data: dict, path: str):
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def evaluate(case: Case, evidence: Evidence):
    if evidence.status != "COMPLETE":
        return {"status": evidence.status, "checks": []}
    checks = []
    for rule in case.expectations:
        actual: Any = None
        if rule.op in {"eq", "gte"}:
            actual = field(evidence.response, rule.path or "")
            if rule.op == "eq":
                passed = (
                    actual is not MISSING and type(actual) is type(rule.expected) and actual == rule.expected
                )
            else:
                passed = type(actual) in {int, float} and actual >= rule.expected
        elif rule.op == "event_count":
            actual = sum(
                1
                for event in evidence.events
                if (rule.step_key is None or event.step_key == rule.step_key)
                and (rule.event_type is None or event.event_type == rule.event_type)
            )
            passed = actual == rule.expected
        else:
            actual = [event.step_key for event in sorted(evidence.events, key=lambda event: event.sequence)]
            iterator = iter(actual)
            passed = all(any(value == expected for value in iterator) for expected in rule.expected)
        checks.append(
            {
                "rule": rule.model_dump(),
                "actual": None if actual is MISSING else actual,
                "missing": actual is MISSING,
                "passed": passed,
            }
        )
    return {"status": "PASS" if all(c["passed"] for c in checks) else "VIOLATION", "checks": checks}


def normalized(evidence: Evidence, ignore_paths: list[str]):
    data = {
        "response": deepcopy(evidence.response),
        "events": [
            {
                "step_key": e.step_key,
                "event_type": e.event_type,
                "payload": deepcopy(e.payload),
                "sequence": e.sequence,
            }
            for e in evidence.events
        ],
    }
    # Paths apply to each response/event payload. Raw evidence remains unchanged.
    for payload in [data["response"], *[event["payload"] for event in data["events"]]]:
        for path in ignore_paths:
            parts = path.split(".")
            parent = payload
            for part in parts[:-1]:
                parent = parent.get(part, {}) if isinstance(parent, dict) else {}
            if isinstance(parent, dict):
                parent.pop(parts[-1], None)
    return data


def compare(case: Case, baseline: Evidence, candidate: Evidence, ignore_paths: list[str]):
    before, after = evaluate(case, baseline), evaluate(case, candidate)
    if before["status"] in {"ERROR", "UNVERIFIED"} or after["status"] in {"ERROR", "UNVERIFIED"}:
        verdict = "EXECUTION_ERROR" if "ERROR" in {before["status"], after["status"]} else "UNVERIFIED"
    elif before["status"] == "VIOLATION":
        verdict = "BASELINE_REVIEW"
    elif after["status"] == "VIOLATION":
        verdict = "REGRESSION_SUSPECTED"
    else:
        verdict = "PASS"
    complete = baseline.status == candidate.status == "COMPLETE"
    return {
        "case_id": case.id,
        "verdict": verdict,
        "baseline": before,
        "candidate": after,
        "different": normalized(baseline, ignore_paths) != normalized(candidate, ignore_paths)
        if complete
        else None,
    }
