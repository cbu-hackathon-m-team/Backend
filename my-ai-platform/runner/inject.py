"""모조 입력 생성과 주입.

1. build_mock_inputs  : 확정된 테스트 스위트 → 실행용 모조 입력 목록 (미확정이면 차단)
2. inject_mock_input  : 모조 입력 한 건을 환경 하나에 넣고 응답을 기록
3. inject_suite       : 모든 케이스를 기준·후보 환경에 같은 순서로 넣음

판정(기대 결과 비교)과 처리 기록 수집은 여기서 하지 않는다.
"""

import copy
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from runner.models import EnvTarget, InjectionResult, MockInput, TestSuite

# 대상 앱이나 모의 서비스가 기록을 실행·환경·케이스별로 구분할 수 있도록 붙이는 헤더
HEADER_RUN_ID = "X-OSSGuard-Run-Id"
HEADER_ENV_ID = "X-OSSGuard-Env-Id"
HEADER_CASE_ID = "X-OSSGuard-Case-Id"


class UnconfirmedSuiteError(Exception):
    """확정되지 않은 테스트 스위트로 실행하려 할 때."""


class EmptySuiteError(Exception):
    """실행할 케이스가 없을 때."""


def build_mock_inputs(suite: TestSuite, run_id: str) -> list[MockInput]:
    """확정된 테스트 스위트를 모조 입력 목록으로 바꾼다.

    입력값은 깊은 복사로 떼어내므로 이후 사용자가 원본 스위트를 수정해도 이번 실행에는 영향이 없다.
    """
    if not suite.confirmed:
        raise UnconfirmedSuiteError(f"test suite {suite.id} is not confirmed")
    if not suite.cases:
        raise EmptySuiteError(f"test suite {suite.id} has no cases")

    return [
        MockInput(
            run_id=run_id,
            suite_id=suite.id,
            suite_version=suite.version,
            case_id=case.case_id,
            input=copy.deepcopy(case.input),
            initial_state=copy.deepcopy(case.initial_state),
        )
        for case in suite.cases
    ]


def _correlation_headers(mock: MockInput, target: EnvTarget) -> dict[str, str]:
    return {
        HEADER_RUN_ID: mock.run_id,
        HEADER_ENV_ID: target.env_id,
        HEADER_CASE_ID: mock.case_id,
    }


def _read_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _describe(e: httpx.HTTPError) -> str:
    if isinstance(e, httpx.TimeoutException):
        return f"timeout: {e!r}"
    return f"{type(e).__name__}: {e}"


async def inject_mock_input(
    client: httpx.AsyncClient, target: EnvTarget, mock: MockInput
) -> InjectionResult:
    """모조 입력 한 건을 환경 하나에 넣는다.

    초기 상태가 있으면 먼저 reset_url로 주입한다. 대상 앱의 응답 코드가 4xx/5xx여도 전달 자체는 성공으로 보고
    그대로 기록한다. 업무 성공 여부는 이후 비교 단계에서 판단한다.
    """
    started_at = datetime.now(timezone.utc)
    headers = _correlation_headers(mock, target)
    base = {"run_id": mock.run_id, "env_id": target.env_id, "case_id": mock.case_id, "started_at": started_at}

    # 1) 초기 상태 주입: 같은 조건에서 시작하지 못하면 결과를 비교할 수 없으므로 입력을 보내지 않는다
    if mock.initial_state:
        if target.reset_url is None:
            return InjectionResult(
                **base, delivered=False, failed_stage="seed",
                error="initial_state is set but the environment has no reset_url",
            )
        try:
            seed_resp = await client.post(
                target.reset_url, json=mock.initial_state, headers=headers, timeout=target.timeout_s
            )
            seed_resp.raise_for_status()
        except httpx.HTTPError as e:
            return InjectionResult(**base, delivered=False, failed_stage="seed", error=_describe(e))

    # 2) 모조 입력 전달
    t0 = time.perf_counter()
    try:
        resp = await client.post(target.input_url, json=mock.input, headers=headers, timeout=target.timeout_s)
    except httpx.HTTPError as e:
        return InjectionResult(
            **base, delivered=False, failed_stage="input", error=_describe(e),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    return InjectionResult(
        **base,
        delivered=True,
        response_status=resp.status_code,
        response_body=_read_body(resp),
        elapsed_ms=(time.perf_counter() - t0) * 1000,
    )


async def inject_suite(
    targets: list[EnvTarget],
    mock_inputs: list[MockInput],
    client: httpx.AsyncClient | None = None,
) -> list[InjectionResult]:
    """모든 모조 입력을 각 환경에 넣는다.

    검증은 1건씩 처리하므로 병렬 실행하지 않는다. 케이스마다 targets 순서(기준 → 후보)대로 같은 입력을 넣는다.
    """
    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        results: list[InjectionResult] = []
        for mock in mock_inputs:
            for target in targets:
                results.append(await inject_mock_input(client, target, mock))
        return results
    finally:
        if own_client:
            await client.aclose()
