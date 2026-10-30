import threading

import httpx
import pytest

from ossguard.mock_service import make_server


@pytest.fixture
def mock_url():
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def test_actual_http_mock_tracks_mutations_and_resets(mock_url):
    with httpx.Client(base_url=mock_url, trust_env=False) as client:
        identifiers = {"run_id": "run1", "env_id": "baseline", "case_id": "T01"}
        headers = {f"X-{key.replace('_', '-')}": value for key, value in identifiers.items()}
        headers["X-Node-Id"] = "inventory_decrement"
        assert client.post("/reset", json={**identifiers, "stock": 10, "paid": True}).status_code == 200
        assert client.post("/inventory", json={"quantity": 2}, headers=headers).json()["stock"] == 8
        headers["X-Node-Id"] = "shipping_request"
        assert client.post("/shipping", json={}, headers=headers).status_code == 200
        state = client.get("/state").json()
        assert state["shipping_count"] == 1
        assert [event["event_type"] for event in state["events"]] == [
            "inventory_decremented",
            "shipping_requested",
        ]
        assert state["events"][1]["node_id"] == "shipping_request"
        assert (
            client.post(
                "/reset", json={**identifiers, "case_id": "T02", "stock": 2, "paid": True}
            ).status_code
            == 200
        )
        assert client.get("/state").json()["events"] == []
        assert client.post("/shipping", json={}, headers=headers).status_code == 409


@pytest.mark.parametrize("paid,stock", [(False, 10), (True, 1)])
def test_failed_payment_or_low_stock_does_not_decrement(mock_url, paid, stock):
    with httpx.Client(base_url=mock_url, trust_env=False) as client:
        client.post(
            "/reset", json={"run_id": "r", "env_id": "b", "case_id": "c", "stock": stock, "paid": paid}
        )
        headers = {"X-Run-Id": "r", "X-Env-Id": "b", "X-Case-Id": "c"}
        assert client.post("/inventory", json={"quantity": 2}, headers=headers).json()["stock"] == stock
        assert not client.post("/inventory", json={"quantity": 2}, headers=headers).json()["applied"]
