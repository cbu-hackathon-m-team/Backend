"""비교·판정 엔진 (F09).

사용자가 확정한 기대 결과(Check)로 각 환경의 관측 결과를 코드로 판정한다. AI 설명과는 분리되어 있다.

케이스 판정
- 기준 통과 · 후보 통과        → pass
- 기준 통과 · 후보 위반        → regression_suspected (회귀 의심)
- 기준부터 위반                → baseline_needs_check (기준 확인 필요, 신규 회귀로 단정하지 않음)
- 어느 한쪽이라도 실행 못 함   → unverified (미검증, 전체 통과로 집계하지 않음)

전후 결과 차이(Difference)는 규칙 위반과 별개로 함께 기록한다.
"""

from collections import Counter
from collections.abc import Iterable
from typing import Any

from runner.models import (
    CaseVerdict,
    CaseVerdictKind,
    Check,
    CheckResult,
    Difference,
    EnvVerdict,
    Observation,
    TestCase,
    ValidationVerdict,
    _is_number,
)

JUDGE_VERSION = "1"

# 값이 없음을 None과 구분하기 위한 표시
MISSING = "<missing>"


# ── 값 경로 ─────────────────────────────────────────────


def _root(obs: Observation) -> dict[str, Any]:
    return {"response": {"status": obs.response_status, "body": obs.response_body}, "state": obs.state}


def resolve(data: Any, path: str) -> Any:
    """"state.stock.SKU-1" 같은 점 경로로 값을 꺼낸다. 리스트는 숫자 인덱스로 접근한다. 없으면 MISSING."""
    cur = data
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return MISSING
    return cur


def _strict_eq(a: Any, b: Any) -> bool:
    # True == 1 같은 혼동을 막는다
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _step_sequence(obs: Observation) -> list[str]:
    return [e.step_key for e in sorted(obs.events, key=lambda e: e.sequence)]


# ── 비교식 평가 ─────────────────────────────────────────


def evaluate_check(check: Check, obs: Observation) -> CheckResult:
    if check.op in ("eq", "gte"):
        actual = resolve(_root(obs), check.target)
        if actual is MISSING:
            return CheckResult(check=check, passed=False, actual=MISSING, reason=f"{check.target} not found")
        if check.op == "eq":
            return CheckResult(check=check, passed=_strict_eq(actual, check.value), actual=actual)
        if not _is_number(actual):
            return CheckResult(check=check, passed=False, actual=actual, reason="actual value is not a number")
        return CheckResult(check=check, passed=actual >= check.value, actual=actual)

    steps = _step_sequence(obs)
    if check.op == "event_count":
        actual = steps.count(check.target)
        return CheckResult(check=check, passed=actual == check.value, actual=actual)

    # event_order: 기대 목록에 있는 단계만 관측 순서대로 뽑아 비교한다 (누락·중복·역순 모두 실패)
    wanted = set(check.value)
    actual = [s for s in steps if s in wanted]
    return CheckResult(check=check, passed=actual == check.value, actual=actual)


def judge_env(case: TestCase, obs: Observation | None, env_id: str) -> EnvVerdict:
    """한 환경의 관측 결과가 케이스의 확정 기준을 모두 만족하는지."""
    if obs is None:
        return EnvVerdict(env_id=env_id, status="not_run", reason="no observation")
    if not obs.delivered:
        return EnvVerdict(env_id=env_id, status="not_run", reason=obs.error or "input was not delivered")
    if not case.expect:
        # 기준 없이 통과시키지 않는다
        return EnvVerdict(env_id=env_id, status="not_run", reason="case has no expected checks")

    results = [evaluate_check(c, obs) for c in case.expect]
    return EnvVerdict(env_id=env_id, status="pass" if all(r.passed for r in results) else "fail", checks=results)


def classify(base: EnvVerdict, cand: EnvVerdict) -> CaseVerdictKind:
    if base.status == "not_run" or cand.status == "not_run":
        return "unverified"
    if base.status == "fail":
        return "baseline_needs_check"
    if cand.status == "fail":
        return "regression_suspected"
    return "pass"


# ── 전후 차이 ───────────────────────────────────────────


def _flatten(data: Any, prefix: str, ignore_keys: frozenset[str], out: dict[str, Any]) -> None:
    if isinstance(data, dict):
        for k, v in data.items():
            if k not in ignore_keys:
                _flatten(v, f"{prefix}.{k}", ignore_keys, out)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _flatten(v, f"{prefix}.{i}", ignore_keys, out)
    else:
        out[prefix] = data


def diff_observations(
    base: Observation, cand: Observation, ignore_keys: Iterable[str] = ()
) -> list[Difference]:
    """기준·후보의 응답·상태·이벤트 건수·이벤트 순서 차이를 구한다.

    ignore_keys에 있는 필드 이름(시간, 임의 ID 등)은 어느 깊이에 있든 비교에서 뺀다.
    """
    ignore = frozenset(ignore_keys)
    b: dict[str, Any] = {}
    c: dict[str, Any] = {}
    _flatten(_root(base), "", ignore, b)
    _flatten(_root(cand), "", ignore, c)

    base_steps, cand_steps = _step_sequence(base), _step_sequence(cand)
    base_counts, cand_counts = Counter(base_steps), Counter(cand_steps)
    for step in base_counts.keys() | cand_counts.keys():
        b[f".events.count.{step}"] = base_counts[step]
        c[f".events.count.{step}"] = cand_counts[step]

    diffs = [
        Difference(path=p.lstrip("."), base=b.get(p, MISSING), cand=c.get(p, MISSING))
        for p in sorted(b.keys() | c.keys())
        if not _strict_eq(b.get(p, MISSING), c.get(p, MISSING))
    ]
    if base_steps != cand_steps:
        diffs.append(Difference(path="events.order", base=base_steps, cand=cand_steps))
    return diffs


# ── 케이스·전체 판정 ────────────────────────────────────


def judge_case(
    case: TestCase,
    base: Observation | None,
    cand: Observation | None,
    ignore_keys: Iterable[str] = (),
) -> CaseVerdict:
    base_v = judge_env(case, base, base.env_id if base else "base")
    cand_v = judge_env(case, cand, cand.env_id if cand else "cand")
    diffs = (
        diff_observations(base, cand, ignore_keys)
        if base is not None and cand is not None and base.delivered and cand.delivered
        else []
    )
    return CaseVerdict(
        case_id=case.case_id, verdict=classify(base_v, cand_v), base=base_v, cand=cand_v, differences=diffs
    )


def judge_validation(
    cases: list[TestCase],
    base: dict[str, Observation],
    cand: dict[str, Observation],
    ignore_keys: Iterable[str] = (),
) -> ValidationVerdict:
    """검증 한 건의 모든 케이스를 판정한다. base / cand는 case_id → 관측 결과."""
    ignore = tuple(ignore_keys)
    verdicts = [judge_case(c, base.get(c.case_id), cand.get(c.case_id), ignore) for c in cases]
    counts = Counter(v.verdict for v in verdicts)

    # 미검증이 하나라도 있으면 전체 통과로 집계하지 않는다
    if counts["regression_suspected"]:
        overall = "regression_suspected"
    elif counts["baseline_needs_check"]:
        overall = "baseline_needs_check"
    elif counts["unverified"] or not verdicts:
        overall = "incomplete"
    else:
        overall = "pass"

    return ValidationVerdict(
        judge_version=JUDGE_VERSION,
        overall=overall,
        counts={k: counts[k] for k in ("pass", "regression_suspected", "baseline_needs_check", "unverified")},
        cases=verdicts,
    )
