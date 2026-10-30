import hashlib
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from runner.models import Result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def execute_http(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    expected_status: int = 200,
    timeout: float = 10,
) -> Result:
    """A single trusted HTTP step, not a business-level regression verdict.

    Request secrets and response bodies are deliberately not persisted by default.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")
    result = Result(kind="http")
    started = time.monotonic()
    try:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
        try:
            response = build_opener(NoRedirect).open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            content = response.read(1_048_577)
            truncated = len(content) > 1_048_576
            result.status = "PASS" if response.status == expected_status and not truncated else "FAIL"
            result.details = {
                "status_code": response.status,
                "expected_status": expected_status,
                "body_sha256": hashlib.sha256(content[:1_048_576]).hexdigest(),
                "body_truncated": truncated,
            }
    except Exception as error:
        result.details = {"error_type": type(error).__name__}
    finally:
        result.duration_ms = round((time.monotonic() - started) * 1000)
    return result
