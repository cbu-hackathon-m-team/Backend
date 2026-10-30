import time
from urllib.parse import urlsplit

from runner.models import Result


def execute_browser(
    url: str, *, selector: str, expected_text: str, timeout_ms: int = 10000, channel: str | None = None
) -> Result:
    """Fresh browser context per run; future workflow actions belong here."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected an HTTP(S) URL without embedded credentials")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    if channel not in {None, "msedge", "chrome"}:
        raise ValueError("Unsupported browser channel")
    result = Result(kind="browser")
    started = time.monotonic()
    try:
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel=channel)
            try:
                with browser.new_context() as context:
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    expect(page.locator(selector)).to_have_text(expected_text, timeout=timeout_ms)
                    result.status = "PASS"
                    result.details = {"assertion": "element_text_matches"}
            finally:
                browser.close()
    except AssertionError:
        result.status = "FAIL"
        result.details = {"assertion": "element_text_mismatch"}
    except Exception as error:
        result.details = {"error_type": type(error).__name__}
    finally:
        result.duration_ms = round((time.monotonic() - started) * 1000)
    return result
