import threading
from unittest.mock import patch

import httpx
import pytest

from ossguard.relay_service import make_server


@pytest.fixture
def relay_url():
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def test_relay_cannot_proxy_arbitrary_destinations(relay_url):
    with patch("ossguard.relay_service.http.client.HTTPConnection") as upstream:
        with httpx.Client(base_url=relay_url, trust_env=False) as client:
            for path in ("/http://example.com", "/target/admin", "/target/health?url=http://example.com"):
                assert client.get(path).status_code == 404
        upstream.assert_not_called()


def test_relay_preserves_request_ids_and_reports_transport_failure(relay_url):
    with patch("ossguard.relay_service.http.client.HTTPConnection") as upstream:
        upstream.return_value.request.side_effect = ConnectionRefusedError()
        with httpx.Client(base_url=relay_url, trust_env=False) as client:
            response = client.post("/target/orders", json={"quantity": 2}, headers={"X-Run-Id": "run-test"})
        assert response.status_code == 502
        assert response.headers["X-OSSGuard-Transport-Error"] == "1"
        assert upstream.call_args.args == ("target", 1880)
        assert upstream.return_value.request.call_args.kwargs["headers"]["X-Run-Id"] == "run-test"
        upstream.return_value.close.assert_called_once()
