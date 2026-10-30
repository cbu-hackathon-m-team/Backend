import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from ossguard.contracts import Environment
from ossguard.node_red import NodeRedAdapter

ROOT = Path(__file__).resolve().parents[1]


async def test_prepare_pins_image_and_rejects_untrusted_image(snapshot):
    client = MagicMock()
    client.images.get.return_value.id = "sha256:fixture"
    client.images.get.return_value.attrs = {"RepoDigests": ["digest"]}
    adapter = NodeRedAdapter(ROOT, client)
    prepared = await adapter.prepare(uuid4(), "baseline", snapshot.baseline, snapshot)
    assert prepared.metadata["image_reference"].startswith("nodered/node-red@sha256:")
    assert len(prepared.artifact_hash) == 64
    assert prepared.metadata["flow_json"]
    snapshot.baseline.image = "untrusted/image:latest"
    with pytest.raises(ValueError):
        await adapter.prepare(uuid4(), "baseline", snapshot.baseline, snapshot)


async def test_docker_start_only_isolated_resources(snapshot):
    client = MagicMock()
    client.images.get.return_value.id = "sha256:fixture"
    client.images.get.return_value.attrs = {}
    container = client.containers.create.return_value
    container.attrs = {
        "NetworkSettings": {
            "Ports": {"8080/tcp": [{"HostPort": "12345"}], "1880/tcp": [{"HostPort": "12346"}]}
        }
    }
    adapter = NodeRedAdapter(ROOT, client)
    environment = await adapter.prepare(uuid4(), "baseline", snapshot.baseline, snapshot)
    adapter._start_containers(environment)
    assert client.networks.create.call_args_list[0].kwargs["internal"] is True
    assert not client.networks.create.call_args_list[1].kwargs.get("internal", False)
    for call in client.containers.create.call_args_list:
        assert call.kwargs["cap_drop"] == ["ALL"]
        assert "environment" not in call.kwargs
        assert all(value[0] == "127.0.0.1" for value in call.kwargs.get("ports", {}).values())
        assert "docker.sock" not in str(call)
    container.put_archive.assert_called_once()
    assert "ports" not in client.containers.create.call_args_list[0].kwargs
    assert "ports" not in client.containers.create.call_args_list[1].kwargs
    assert "relay" in client.containers.create.call_args_list[2].kwargs["name"]


def test_missing_gateway_binding_is_a_clear_error():
    container = MagicMock()
    container.attrs = {"NetworkSettings": {"Ports": {"8080/tcp": []}}}
    with pytest.raises(RuntimeError, match="Gateway host port"):
        NodeRedAdapter._url(container, "8080/tcp")


async def test_cleanup_filters_exact_run_and_retries_failures():
    client = MagicMock()
    container = MagicMock()
    container.id = "leftover"
    container.remove.side_effect = RuntimeError("busy")
    client.containers.list.return_value = [container]
    client.volumes.list.return_value = []
    client.networks.list.return_value = []
    adapter = NodeRedAdapter(ROOT, client)
    run_id = uuid4()
    outcome = await adapter.cleanup(run_id)
    assert outcome.status == "PENDING"
    assert outcome.remaining == ["leftover"]
    assert f"ossguard.run={run_id}" in client.containers.list.call_args.kwargs["filters"]["label"]
    container.remove.side_effect = None
    assert (await adapter.cleanup(run_id)).status == "COMPLETE"


async def test_cancellation_waits_for_inflight_docker_work():
    adapter = NodeRedAdapter(ROOT, MagicMock())
    started, finish = threading.Event(), threading.Event()
    completed = []

    def operation():
        started.set()
        finish.wait(2)
        completed.append(True)

    task = asyncio.create_task(adapter.blocking(operation))
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed == [True]


async def test_httpx_reset_execute_collect_and_node_mapping(snapshot, monkeypatch):
    run_id = str(uuid4())
    environment = Environment(
        run_id=run_id,
        env_id="baseline",
        image="image",
        artifact_hash="hash",
        adapter_version="1",
        metadata={
            "mock_url": "http://mock",
            "target_url": "http://target",
            "request_timeout": 5,
            "step_map": snapshot.step_map,
        },
    )
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/reset":
            assert json.loads(request.content)["stock"] == 10
            return httpx.Response(200, json={})
        if request.url.path == "/orders":
            assert request.headers["x-case-id"] == "T01"
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(
            200,
            json={
                "run_id": run_id,
                "env_id": "baseline",
                "case_id": "T01",
                "stock": 8,
                "shipping_count": 1,
                "events": [
                    {
                        "node_id": "shipping_request",
                        "event_type": "shipping_requested",
                        "payload": {},
                        "sequence": 1,
                    }
                ],
            },
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        "ossguard.node_red.httpx.AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )
    adapter = NodeRedAdapter(ROOT, MagicMock())
    evidence = await adapter.execute(environment, snapshot.cases[0])
    evidence = await adapter.collect(environment, evidence)
    assert evidence.status == "COMPLETE"
    assert evidence.events[0].step_key == "shipping"
    assert evidence.events[0].node_id == "shipping_request"
    assert [request.url.path for request in requests] == ["/reset", "/orders", "/state"]


def test_sample_diff_is_only_injected_shipping_edge():
    normal = json.loads((ROOT / "fixtures/node-red/normal.json").read_text())
    broken = json.loads((ROOT / "fixtures/node-red/missing_shipping.json").read_text())
    fixed = json.loads((ROOT / "fixtures/node-red/fixed.json").read_text())
    assert normal == fixed
    differences = [(a, b) for a, b in zip(normal, broken, strict=True) if a != b]
    assert len(differences) == 1
    before, after = differences[0]
    assert before["id"] == after["id"] == "inventory_route"
    assert before["wires"][0] == ["shipping_setup"]
    assert after["wires"][0] == ["finish"]
