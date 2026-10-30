"""Explicit browser integration check, requires installed Chromium."""

import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from runner.browser import execute_browser

handler = functools.partial(
    SimpleHTTPRequestHandler, directory=str(Path(__file__).resolve().parents[1] / "fixtures/site")
)
server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    url = f"http://127.0.0.1:{server.server_port}"
    channel = os.environ.get("BROWSER_CHANNEL")
    success = execute_browser(url, selector="h1", expected_text="Runner ready", channel=channel)
    mismatch = execute_browser(
        url, selector="h1", expected_text="Wrong text", timeout_ms=500, channel=channel
    )
    assert success.status == "PASS", success.to_dict()
    assert mismatch.status == "FAIL", mismatch.to_dict()
    print("Browser smoke: PASS (matching and mismatching text)")
finally:
    server.shutdown()
    server.server_close()
    thread.join()
